> Component guide: run commands below from `sdk/`. Repository policies also apply from the root.

# Maintainer protections

These are required operating guidelines. Files such as CODEOWNERS describe review
ownership; they do not enforce GitHub settings. Verify the live settings before
source publication and releases, and record verified controls and remaining checks
in private release evidence. An unchecked item below is a verification step, not a
claim that the control is absent. A configured origin does not establish protections
or authorize a source push.

## Before the first source push

- [ ] Review the complete initial tree and artifacts for secrets, private history,
  backend code, production configuration and customer data. Scan staged content;
  do not rely only on filename ignores or secret-pattern detection.
- [ ] Verify repository admin/write access and organization 2FA requirements.
  Limit write access to maintainers; use least-privilege GitHub App permissions.
- [ ] Enable private vulnerability reporting and subscribe responsible maintainers
  to reports. Verify the private reporting path works before directing reporters to it.
- [ ] Enable secret scanning, push protection and dependency vulnerability alerts
  where available; record any unavailable control and an explicit alternative.
- [ ] Restrict Actions to approved actions, pin third-party actions by full commit
  SHA, default GITHUB_TOKEN to read-only, and require approval for external
  contributors' workflow runs. Do not send fork jobs secrets or publishing tokens.
- [ ] Prepare main and release-tag rulesets before the initial push where supported;
  verify matching and enforcement as soon as refs exist. Allow only a narrowly
  scoped initial-ref exception if needed, then remove it and record verification.

## Main and release protection

Require pull requests, resolved conversations and squash merges on `main`. During
[solo maintenance](../MAINTAINERS.md#solo-maintainer-review), require zero approving
reviews and do not require code-owner or latest-push approval. The maintainer must
review the final diff and verification evidence before merging their own work.
Prohibit force pushes and deletion, with no routine bypass actors.

Configure required CI checks after successful runs establish their actual names;
do not create fictitious requirements. Until those checks are enforced, inspect
all applicable workflow results before merging. Bootstrap of an empty default
branch requires separate source-push authorization; if a temporary ruleset exception
is necessary, record it, limit it to initial ref creation and restore enforcement
immediately afterward. Adding a second eligible maintainer requires revisiting
review requirements and enabling independent approval.

Restrict `python-v*` and `typescript-v*` tag creation to repository administrators.
Use a separate ruleset without bypass actors to prevent tag updates and deletion.
Keep release version, tag, commit and artifact hashes consistent.
Check actual GitHub feature/plan support and enforcement; no protection is active
merely because this document says it is required.

## Workflows and publication

Run pull-request tests on disposable hosted runners without production access.
Never use pull_request_target or workflow_run to check out and execute untrusted
PR code with secrets or a write token. Avoid shared self-hosted runners for public
contributions. Treat package install hooks and modified build scripts as untrusted.
Do not interpolate PR titles, branch names or other untrusted strings into shell
source. Do not promote caches/artifacts from untrusted jobs into a trusted release.

Use separate Python and TypeScript release workflows. Prefer registry Trusted
Publishing bound to the exact repository, workflow and protected environment.
Grant id-token: write only to the publishing job; keep build/test jobs unprivileged.
No AWS production credentials belong in SDK CI. A release environment must require
explicit approval and restrict permitted release tags. The `pypi` environment names
`@vasu-openeight` as its reviewer, permits self-approval during solo maintenance,
blocks administrator bypass and permits only `python-v*` tags. Approval applies to
the displayed run's source and artifacts. Do not bypass a missing gate.

Review the final source revision, license/NOTICE, dependency/advisory results,
installed-package tests and distribution inventory. Promote only the qualified
artifacts and verify their hashes after download and registry upload. Keep skipped
backend/provider checks visible. Never overwrite a published version: issue a new
version and consider yanking/deprecating the defective release.

## Public incidents

If a secret is exposed, revoke or rotate it promptly through the owning private
system and coordinate incident handling privately. Removing a file or rewriting
history does not revoke copies already downloaded. Keep customer or exploit details
out of public issues and CI logs; publish an advisory once a coordinated fix is ready.

GitHub references: [secure workflow use](https://docs.github.com/en/actions/reference/security/secure-use)
and [private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

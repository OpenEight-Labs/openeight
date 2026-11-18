> Component guide: run commands below from `sdk/`. Repository policies also apply from the root.

# Repository guide

This public repository owns OpenEight's Python and TypeScript SDKs, shared public
contract fixtures and client-side tests. Read README.md, CONTRIBUTING.md and the
applicable package README before changing code. Keep each SDK independently
versioned and licensed under Apache-2.0. MAINTAINERS.md owns release and repository
protection procedures. Follow the repository-wide [security policy](../SECURITY.md).

## Public boundary

Everything tracked, uploaded as an artifact, or printed in public CI may become
permanently available. Copy only reviewed SDK material into this repository.
Never import private repository history, backend implementation, deployment/IAM
configuration, operational evidence, customer records, production logs, credentials,
private keys or local application state. Use synthetic fixtures and reserved example
domains. Inspect diffs and artifact contents; .gitignore is not a security boundary.
Do not rename existing public wire fields merely to hide an internal product name.

## Engineering and verification

- Preserve public API compatibility, explicit credentials/provider configuration,
  human/machine separation, scope validation and fail-closed behavior.
- Keep Python changes in python/ and TypeScript changes in typescript/. Keep shared
  protocol declarations in contracts/. Generate error mappings with
  scripts/generate_error_catalog.py and check drift with its --check option.
- Keep the public package agent-only. Do not import backend OpenAPI/REST schemas,
  Console session clients or administration routes. Keep contracts/agent-api.json
  consistent with both SDKs and run scripts/check_public_surface.py. Validate the
  Python compatibility registry with scripts/check_framework_registry.py.
- Do not add implicit provider/model/credential fallbacks. Keep optional content capture
  opt-in.
  Do not log secrets, raw provider bodies or customer model/tool content.
- Run focused tests, practical package checks, generated drift and whitespace checks.
  Test the installed distribution for release-affecting packaging changes.
- State skipped coverage and missing external inputs. Standalone tests, backend
  integration, live-provider acceptance and package publication are separate evidence.

## Python style and comments

Follow [PEP 20 — The Zen of Python](https://peps.python.org/pep-0020/).
Prefer readable, explicit code, simple control flow and clear module boundaries.
Avoid unnecessary abstractions, clever shortcuts and deep nesting. Handle errors
explicitly; do not guess missing configuration or silently suppress failures.
Apply these principles practically while preserving security and compatibility.

Across both SDKs, keep comments and docstrings short and precise. Document public
contracts and explain non-obvious constraints, security checks, concurrency and
recovery behavior. Avoid restating code, implementation history and narration.
Use examples or contributor documentation for longer explanations.

## Contributions and releases

Use a focused feat/, fix/, chore/ or docs/ branch and a reviewed pull request.
Preserve unrelated work. Follow the
[solo-maintainer review policy](../MAINTAINERS.md#solo-maintainer-review) for
authentication, transport, telemetry disclosure, durable recovery, public contracts
and release workflow changes. Untrusted pull-request code must not receive release credentials,
OIDC publishing authority or access to protected infrastructure.

Never commit to main, push code, create/move release tags, publish a package, change
repository visibility, or modify repository protections without explicit owner
or user authorization. Review-only requests do not authorize edits or publication.
Do not upload source to external review services without explicit authorization.

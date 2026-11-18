# Repository guide

OpenEight owns the public SDKs and runnable cookbook examples. The hosted control
plane and public skill source belong to separate repositories.

## Read before editing

[CONTRIBUTING.md](CONTRIBUTING.md) owns the shared contribution process;
[RELEASING.md](RELEASING.md) owns release gates. Follow [security guidance](SECURITY.md)
and preserve the [Apache-2.0 license and attribution notices](README.md#licensing).
Read [sdk/AGENTS.md](sdk/AGENTS.md) and the relevant package README for SDK changes,
or [cookbook development](cookbook/CONTRIBUTING.md) for recipes.

| Path | Responsibility |
| --- | --- |
| `sdk/python/`, `sdk/typescript/` | Independently versioned clients and tests |
| `sdk/contracts/` | Shared public contracts and error catalog |
| `sdk/scripts/` | SDK generators, surface checks and qualification tools |
| `cookbook/` | Business agents, integration patterns and launcher |
| `scripts/`, `.github/` | Repository checks and CI |

Run SDK tools from `sdk/` and cookbook commands from `cookbook/`. Generate error
mappings with `python scripts/generate_error_catalog.py` from `sdk/`; do not edit
generated mappings by hand. Keep public contracts agent-only and cookbook code
on public SDK interfaces.

## Work and verification

Use a focused `feat/`, `fix/`, `chore/` or `docs/` branch; never commit to `main`.
Preserve unrelated changes, private configuration and durable runtime state.
Never bypass denial, approval, scope validation or missing readiness. Keep content
capture opt-in and provider/model/credential configuration explicit.
Follow the [solo-maintainer review policy](MAINTAINERS.md#solo-maintainer-review)
for contracts, security and release changes; record review and verification evidence.

Use Python 3.12 for the current qualification lane and Node 24 for TypeScript.
From the root, run `python scripts/check.py`; this is a structural and selected
cookbook check, not the full test suite. Follow the
[change-to-check table](CONTRIBUTING.md#development-and-verification) for affected suites.
Check whitespace with `git diff --check`; inspect untracked files separately.
Report commands, runtime versions, results and skipped checks with reasons. Keep
local candidate tests, backend/provider acceptance and registry acceptance distinct.

No pushes, tags, publication, live runs or repository administration without
separate authorization. Do not send source to external review services without
explicit authorization. Changes here do not authorize modifications to other
checkouts, their private configuration, durable state or repository settings.

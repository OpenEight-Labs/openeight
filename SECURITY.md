# Security policy

This policy covers the Python and TypeScript SDKs, cookbook applications and
patterns, shared contracts, tests, and repository build/release tooling.
OpenEight maintainers own security triage for this repository.

## Reporting a vulnerability

Do not disclose vulnerabilities, working exploits, credentials or customer data
in public issues or pull requests. When enabled, use this repository's
**Security → Report a vulnerability** option for a private report. If unavailable,
contact [hello@openeight.ai](mailto:hello@openeight.ai) only to request a private security reporting channel;
do not include exploit details or secrets in that initial message.

Before public release, maintainers must verify a monitored private reporting
route, enable and monitor GitHub private vulnerability reporting where supported,
and document a verified fallback. Record the verification date and responsible
maintainer in private release evidence. A listed address alone does not establish
a working reporting channel. Do not send sensitive details until the private
route has been confirmed.

Through the confirmed private channel, include:

- Affected component, package version or source commit, runtime and relevant dependencies.
- Prerequisites and reproducible steps using synthetic data, with expected and actual behavior.
- Concrete impact, affected trust boundary and any safe workaround you have identified.

Report only what is necessary to reproduce the issue. Do not attach live tokens,
customer records or private operational logs. Do not test against other users,
production systems or live business services without their explicit authorization.
If a credential is exposed, coordinate revocation or rotation through its owner;
removing a file does not invalidate downloaded copies.

## Support and coordinated disclosure

Released security-support commitments have not yet been established. Local
candidates are under qualification and carry no production-support commitment.
Before the first package release, maintainers must publish the supported package
versions, Python/Node runtimes and framework combinations in the release notes,
including how unsupported versions should upgrade. Do not infer managed-framework
support from Python package metadata or a successful installation. See
[compatibility qualification](sdk/python/constraints/README.md).

Maintainers should acknowledge private reports, investigate reachability and
impact, coordinate a fix and disclosure with the reporter, and communicate affected
versions and upgrade guidance. There is no guaranteed response or remediation
time and no support SLA in this policy. Keep exploit details private during
coordination. Publish an advisory and release notes when a fix or coordinated
mitigation is ready, with enough information for users to assess exposure.
Security fixes use new package versions; never replace published files or move
release tags. Follow [release and recovery procedures](RELEASING.md).

## Scope and trust boundaries

The SDKs provide agent registration, readiness checks, Decisions, approval-state
polling, telemetry and Model Context Protocol (MCP) tool integrations over HTTP
and stdio subprocesses. MCP servers supply untrusted tool catalogs and results;
configured subprocesses execute with the host process's operating-system authority.
An explicit subprocess environment does not provide a sandbox. Cookbook includes
local HTTP applications, business tools, provider integrations and
persistence/recovery examples. The hosted control plane
and independently maintained public skills are outside this source tree; a report
that crosses those boundaries should still identify the affected interaction so
maintainers can coordinate with its owner.

Applications own their credentials, configured endpoints, provider choices,
custom transports, destination adapters and persistence integrations. SDK selectors
and local customer confirmation do not establish server-side authority. Synthetic
sample data do not make integrated execution, HTTP interfaces or stored state safe
by definition. Local examples are not a production deployment/security template.

Treat endpoint configuration, HTTP requests, API responses, tool schemas/arguments,
model/tool content, restored state, dependencies and pull-request changes as
untrusted inputs. Protect credentials, session identifiers, customer content,
organization/workspace and business-object scope, governed-operation identity,
durable state and package publishing authority.

## Required security properties

- Keep credentials within their intended transport and origin. Do not log secrets
  or send them to redirects or unintended hosts. Preserve each SDK's documented
  endpoint/TLS policy; do not weaken validation to make tests or integrations pass.
- Preserve machine API-key authentication, organization/workspace scope and
  response-identity validation. SDKs must not provide human-session authority.
  Client selectors are not authorization.
- Fail closed on malformed protocol data, incompatible runtime state, missing
  required bindings and authorization uncertainty. For operations configured for
  governance enforcement, denied or pending approval, missing readiness, and
  unavailable governance must not execute protected effects.
  Keep customer confirmation, OpenEight authorization and destination receipts distinct.
- Native and observation-only paths do not confer OpenEight authorization and do
  not require governance enforcement merely because they execute tools. They remain
  subject to credential, content, scope and transport requirements. A governed path
  must not silently fall back to either mode when authorization is unavailable.
- Preserve MCP credential isolation between the control plane and tool sources,
  explicit subprocess environments without ambient environment inheritance,
  redirect rejection and bounded discovery and message parsing. Bind remote dispatch
  to the selected source, tool binding, invocation and exact arguments through
  single-use permits. Changed or revoked bindings must not admit new dispatch;
  invalidation cannot undo a call already admitted to the destination.
- Keep provider/model/credential configuration explicit and optional telemetry
  content capture opt-in. Separate required governance input from optional
  model/tool content; sanitize fixtures, diagnostics and logs.
- Preserve immutable retry identities, settlement ordering, bounded queues,
  cancellation/close behavior and durable recovery checks. Do not claim exactly-once
  business effects without destination-owned idempotency and reconciliation.
  Preserve pending work and its recovery identity; do not discard or silently migrate state.
- In cookbook integrations, validate customer/case/order scope and required customer
  confirmation before business effects. Protect state files and local HTTP controls
  from unintended access. Use explicit authenticated adapter boundaries for real
  services, and keep automatic tests on synthetic data and deterministic service doubles.
- Keep untrusted contribution tests separate from release credentials and protected
  infrastructure. Use read-only CI tokens and disposable runners for PR checks.
  Publish only reviewed, qualified artifacts with verified hashes.

## Reportability and limitations

Report reachable credential/content disclosure, cross-scope or identity confusion,
authorization bypass, unsafe HTTP controls, unsafe recovery/retry behavior,
meaningful resource exhaustion, and package or workflow compromise. Rate severity
from actual reachability and impact, including prerequisites and deployment context,
not the presence of a pattern. Cookbook findings are not excluded because the
bundled business data are synthetic. No blanket finding exclusions or accepted-risk
waivers are established by this policy.

Standalone fixture tests, dependency scans, code review, backend integration and
real-provider acceptance are separate evidence. A passing check does not suppress
an otherwise reachable vulnerability. External qualification inputs, skipped
coverage and unverified operational controls must remain visible in release evidence.

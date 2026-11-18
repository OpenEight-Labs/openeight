# Changelog

## Unreleased — 0.0.1

Initial public Python SDK release, licensed under Apache-2.0. Release notes describe
the public package; hosted-service terms and third-party dependency licenses remain
separate.

### Agent APIs

- Agent-scoped clients for tool registration, readiness, Decisions and telemetry.
- Immutable Decision preparation and exact dispatch, Run identity preparation,
  checkpoint import/export, suspension and resumption.
- Validation of Event payloads, enum values, Unicode and Tool/Decision references
  before queue admission; preservation of valid JSON nulls and protocol extensions.
- Retryable delivery and completion with retained queued Events, explicit lifecycle
  ownership and bounded cleanup that reports incomplete settlement.

### Managed integrations

- Native-shaped LangChain and Deep Agents constructors configured with
  `openeight=OpenEightConfig(...)`, plus observation-only adapters.
- LangChain/LangGraph dependencies included by default; optional Deep Agents, MCP
  and durable-checkpoint integrations use their declared extras.
- Framework compatibility checked against the packaged qualification registry.
  See [compatibility guidance](constraints/README.md) for the tested runtime matrix
  and the distinction between core Python support and managed integrations.
- Durable execution and approval recovery with scope/readiness validation, explicit
  checkpoint ownership and retained execution identity. Business effects still
  require destination-owned idempotency receipts and reconciliation.

### Security and telemetry

- Remote endpoints require HTTPS; plaintext HTTP is limited to explicit loopback
  addresses. Default transports ignore ambient proxy/CA overrides and reject redirects.
- Optional model/tool content capture defaults to `none`, with explicit capture
  modes and application redaction. Required governance inputs are separate from
  optional telemetry capture.
- Decision-free read Events carry `openeight.tool.governance_revision_id`; the
  server must support that attribute and its revision validation.
- Public errors avoid exposing transport details, credentials and sensitive locals.

### Packaging

- Inline Python type information, a `py.typed` marker and preserved direct-client
  signatures for type checkers.
- Wheel and source distributions include Apache-2.0 licensing and copyright notices.
- Tests and qualification constraints remain in the repository rather than the
  published distributions.

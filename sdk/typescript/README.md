# OpenEight TypeScript SDK

[Website](https://openeight.ai) · [Documentation](https://openeight.ai/docs)

The `@openeight/sdk` package provides agent registration, readiness,
Decisions, approval-state polling, and Run/Event telemetry over OpenEight Connect
JSON. Configure an external endpoint, Workspace API key and Agent name. This
package contains no Console session, organization administration, human approval
authoring or Assist administration client.

Requests and response types are implemented in the SDK. The checked-in
`../contracts/agent-api.json` lists supported operations only; the complete
backend REST/OpenAPI contract is private and is not needed to build this package.

## Installation

This package currently has `private: true` and is not enabled for npm publication.
To develop and test it from this checkout, follow [Qualification](#qualification).
Registry installation instructions will be added when a public release is available.

## Machine client

Use Node 24 for machine integrations. A machine client requires the OpenEight
origin, a current Workspace API key (`ctk_...`), and the Agent name. Identity
and Workspace authority are resolved by the server from that credential:

```ts
import { MachineClient, ToolRegistration, collectSelectedValues } from "@openeight/sdk";

const client = new MachineClient({
  baseUrl: process.env.OPENEIGHT_ENDPOINT!,
  apiKey: process.env.OPENEIGHT_API_KEY!,
  agentName: process.env.OPENEIGHT_AGENT_NAME!,
});

try {
  const refund = new ToolRegistration({
      name: "refund.create",
      description: "Create a refund",
      inputSchema: {
        type: "object",
        properties: { orderId: { type: "string" }, amount: { type: "integer" } },
        required: ["orderId", "amount"],
        additionalProperties: false,
      },
    });
  const [registration] = await client.registerTools([refund]);
  if (!registration?.tool) throw new Error("Tool registration was rejected");

  const selected = registration.tool;
  const readiness = (await client.getToolReadiness([selected.toolVersionId])).tools[0];
  if (!readiness?.eligible || !readiness.policyReady || !readiness.policyContractSetDigest) {
    throw new Error("Configure the native Tool and approve its policies before execution");
  }
  const arguments_ = { orderId: "order-42", amount: 500 };
  const runIdentity = client.prepareRunIdentity();
  const run = await client.governedRun(runIdentity);
  try {
    const prepared = client.prepareDecision({
      runId: run.runId,
      toolId: selected.id,
      toolVersionId: selected.toolVersionId,
      toolName: selected.name,
      connector: "billing",
      action: "refund.create",
      input: {},
      policyContractSetDigest: readiness.policyContractSetDigest,
      selectedValues: collectSelectedValues(arguments_, readiness),
    });
    const decision = await client.decide(prepared);
    if (decision.verdict === "allow") {
      // Perform the external effect only after this explicit check.
      run.submitEvent({
        name: selected.name,
        observation_kind: "tool_call",
        operation_kind: "invoke",
        occurred_at: new Date().toISOString(),
        outcome: "success",
        decision_id: decision.decisionId,
        tool_id: selected.id,
        tool_version_id: selected.toolVersionId,
        tool_name: selected.name,
        input: {
          input: prepared.input,
          policy_contract_set_digest: prepared.policyContractSetDigest,
          selected_values: prepared.selectedValues,
        },
      });
    }
    // For approval_required, poll approvalState until a human approves, then replay
    // this exact prepared Decision. Require replayed=true, matching IDs and
    // verdict approval_required or allow; approval does not change the stored verdict.
    await run.close();
  } catch (error) {
    // Keep the original Run identity if the response may have been lost.
    throw error;
  }
} finally {
  await client.close();
}
```

PolicyVersion owns input selectors; native Tool handles expose `toolContractDigest`
and native schema facts. Registration provides readiness summaries. Fetch exact-version
readiness before each new Write preparation and collect the minimal native path union
with `collectSelectedValues`. Explicit empty selections are valid. `prepareDecision`
freezes the complete input envelope; reuse it unchanged for retries, approval resume,
and linked telemetry. A stale contract requires fresh preparation with a new Decision ID.
For a changed schema, supply both `expectedToolId` and
`expectedCurrentVersionId` from the last acknowledged catalog state. Omit both
for first discovery. Preserve the same pair on transport retry; a conflict requires
operator reconciliation. Each live Tool exposes `currentVersionId`; configuration
and schema successors preserve its Tool ID. Historical schemas cannot be restored
through registration.
Keep a `PreparedDecision` and `RunIdentity` until their outcomes are known.
After `approval_required`, approval state alone is not permission to act: wait
for `approved`, replay the exact prepared Decision, and require the replayed
verdict to be `approval_required` or `allow`, with `replayed=true` and matching
identities. Approval does not change the stored verdict. Other states/verdicts
and replay errors refuse execution. The SDK never executes a callable or retries a
Decision automatically.

`GovernedRun` owns bounded, in-memory Event buffering. `submitEvent` accepts
local queue ownership; `flush` confirms server acknowledgements. `suspend`
flushes and returns an application-owned checkpoint while the backend Run
remains active. Resume only with exclusive application ownership and before
any completion attempt. `close` flushes Events and then sends one retained Run
completion. Keep exact lifecycle facts across process loss; checkpoints do not
provide distributed fencing or durable recovery.

The default buffer admits at most 1,000 records and 8 MiB, including retained Run
identities. It flushes at 100 Events, 3 MiB or one second. Each request is limited
to 1,000 Run/Event records and 4 MiB. Configure these bounds through `observe` on
`MachineClient`. Queue exhaustion raises `ObserveQueueFullError`; timer failures
retain queued Events, while explicit `flush` and `close` report delivery failures.
Cleanup requests default to a ten-second timeout. Retry a failed `run.close()`
before closing its client, retaining the same Run identity. A failed
`client.close()` may already have closed its writer and transport, so retryable
settlement is not guaranteed. The buffer does not persist across process loss.

Completion captures the later of startup and the local clock once, avoiding a
negative duration after clock skew. Retries keep that timestamp unchanged.

## Workflow and checkpoint transfer

`prepareDecision` and `prepareRunIdentity` accept paired `workflowId` /
`workflowVersion`: canonical UUIDv5/v7 and SemVer up to 128 UTF-8 bytes. These are
correlation references, not authorization. Retain the same authenticated API-key
identity for Decision retries; the same Agent name alone does not preserve its
server fingerprint after key rotation.

Use `exportRunCheckpoint(checkpoint)` and `importRunCheckpoint(value)` for validated,
network-free snake_case checkpoint data shared with Python. New checkpoints retain
`attributes_json` exactly, including numeric spelling and object-key ordering.
Runtime `attributesJson` remains optional for legacy structural callers. Missing
legacy encoding stays absent on export; re-encoding cannot guarantee cross-language
replay identity. Resume and completion preserve the original encoding when present.
Checkpoints contain neither credentials, an Event outbox nor execution authority.
Resume only with exclusive application ownership and original endpoint/Agent/Workspace
authority. Repeated suspension is allowed; completed or completion-attempted Runs
cannot resume. Metadata import does not establish the remote Run status.

All SDK leaf errors inherit `SDKError` and preserve existing names, messages and
fields. Native programming errors are not automatically wrapped.

Raw `writeTelemetry` retains valid extra wire fields. Metric fields use canonical
nonnegative decimal strings through int64 maximum; JSON structured numbers must
fit JavaScript's exact safe-integer subset, ±(2**53 - 1). Python also supports
integers outside this common subset; no implicit string conversion is added.
Native Run/Event IDs require UUIDv7; raw parent-Run, parent-Event and Decision
references accept UUIDv5/v7. Run writes require status/start time and ordered end
time for completion. Event kind/operation/outcome, span roles, tool references,
payload combinations and token/cache/timing arithmetic are validated before queue
admission. Extra fields are forwarded without granting authority; server-owned
selector fields at the record root are rejected. Existing JSON wire names and
Observe limits remain unchanged. Compatibility declarations and managed integrations
are unchanged.

## Qualification

```sh
npm ci
npm run build
npm test
python3 ../scripts/check_public_surface.py
```

The build emits JavaScript and type declarations into `dist/`. npm publication
requires a separately reviewed release and removal of the private-package flag. Configure policies, approval channels
and credentials through the OpenEight Console; an agent can poll approval state
but cannot approve its own request.

## Admission and identity

Machine Connect errors preserve supported admission reasons. Treat rejected
readiness or Decisions as refusals; do not dispatch the protected external action.
The SDK cannot stop application code executing outside its governed boundary.

Optional `integration` is one of `otel`, `langchain`, `deepagents` or `other`.
It is copied before requests and sent as `X-OpenEight-Agent-Integration`; omission
remains absent. Existing identity mismatches are rejected, never silently rebound.

Timestamp validation rejects impossible calendar dates instead of rolling them
forward. Output-token counts use the same non-negative int64 validation as other
token metrics. Tool-schema regex restrictions apply to both `pattern` values and
`patternProperties` keys.

Direct Event admission distinguishes omitted payload fields from encoded JSON `null`.
Error outcomes require an error payload and forbid output; success forbids an error
payload. Interrupted Events forbid output/error, and lifecycle Events forbid all
input/output/error payloads. Spans and LLM Events cannot carry Tool or Decision
references. Tool calls require a tool name. Decision-linked tool calls require a
registered Tool/ToolVersion pair, a name, a success/error outcome and object input.
Identified lifecycle Events require the registered pair and name; a bare lifecycle
Event remains valid. Invalid records are rejected before queue ownership so valid
Events can still flush and complete their Run. Recognized ProtoJSON camelCase
aliases receive the same validation; sending both spellings of one field is rejected.

## License

Copyright © 2026 OpenEight Systems Private Limited. Licensed under the
Apache License, Version 2.0. See the included LICENSE and NOTICE files.
The SDK license does not grant access to OpenEight hosted services, which remain
subject to separate service terms.

## Version compatibility

Initial `0.0.x` releases are experimental and may introduce breaking changes.
Pin qualified versions and read the [versioning policy](../../docs/versioning.md) before upgrading.

## Development checks

Run `npm run format:check`, `npm run typecheck`, `npm run build` and `npm test`.
Use `npm run format` to format handwritten source and tests. Generated error
mappings retain their generator's formatting.

Use `await client.close()` to drain owned telemetry and close the client.
Transport shutdown is internal and is not exposed as a separate client method.

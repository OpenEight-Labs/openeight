# Raw telemetry acceptance

`write_telemetry` preserves valid extra wire fields and Python integer or canonical
decimal-string int64 metrics. Run rows require native UUIDv7 identity, status and
start time; completion also requires an end time at or after startup. Event rows
require native UUIDv7 Event/Run IDs, name, kind, operation, outcome and occurrence
time. Parent-Run, parent-Event and raw Decision references accept UUIDv5/v7.
Typed `Event` retains its existing UUIDv7 correlation rules.

Supported kinds are `span`, `llm_call`, `tool_call`, `lifecycle`; operations are
`unspecified`, `invoke`, `stream`; outcomes are `unspecified`, `success`, `error`,
`interrupted`. Span roles, tool references, payload combinations and token/cache/
timing arithmetic are validated before admission. JSON payload fields retain the
wire names `input_json`, `output_json`, `error_json`, `attributes_json`.
Unknown fields are forwarded without becoming SDK authorization or identity fields.
Server-owned authority selectors are rejected at the record root; names inside
application JSON remain data. Requests remain limited to 1,000 records and 4 MiB.

The shared structured-integer range is ±(2**53 - 1). Python retains its existing
int64 support outside that portable subset. No implicit string conversion is added.

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

See [direct request recovery](direct-recovery.md) for retry and checkpoint rules,
and the [SDK overview](../README.md) for managed integrations.

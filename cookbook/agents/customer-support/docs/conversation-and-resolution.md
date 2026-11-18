# Conversation and case resolution

A support conversation should be able to explain progress without requesting
permission to speak. The prior `send_customer_reply` tool also mutated case status,
which coupled ordinary customer replies to business authorization and closure.
Customer Support now uses native assistant responses for conversation and a
separate `resolve_case` Write for closure. No outbound email or SMS is implemented.

## Ownership and flow

The graph produces ordinary assistant messages. The existing command settlement
stores visible turns in `conversation_turns`; no retailer message or business
receipt is created for chat. LangChain/OpenEight retain their existing transcript,
checkpoint and telemetry behavior. Content capture remains opt-in separately from
local conversation persistence.

The model reads current remedy progress. Once a remedy and any required refund
settlement are complete, it may call `resolve_case(case_id, action_id,
facts_revision)`. These are required native fields: two nonempty scoped string
identifiers and an integer revision of at least one. OpenEight owns the decision,
including deny or approval if the operator's active policy requires it. Assistant
output cannot substitute for authorization or change case status.

The retailer validates the case and action against its bound deployment, customer
and order, and compares `facts_revision` with the current order revision under the
writer transaction. The action must be completed. If it is a child settlement, its
allocated parent must also be completed. A confirmed/requested/dispatched remedy,
a return awaiting inspection or a positive unsettled cancellation/return cannot
resolve the case. A completed replacement does not require an additional refund.

Successful resolution sets the case to `resolved`, advances the facts revision and
inserts one closure receipt atomically. The existing unique case-closure constraint
prevents a second receipt. Exact frozen-request/idempotency-key replay returns the
first receipt before terminal/revision checks; changed-envelope reuse conflicts.
The agent reports resolution only after successful business evidence. Handoff sets
`handed_off` and ends the business conversation with an ordinary acknowledgement;
it does not produce closure or claim resolution.

## Local state and compatibility

This workflow uses deployment `schema_version=4` in `deployment.json` and both
SQLite deployment records. Their existing field names/types, scope keys and
credential-binding semantics remain unchanged; the version check now requires 4.
There is no automatic conversion. Versions 2/3 are rejected before their journal is
initialized or commands are executed. Keep pending work on its original runtime,
key and directory, and use a fresh state directory and separately configured Agent
for version 4. Changing policy does not rewrite an existing pending Decision.

No new tables or columns are introduced. Existing `cases.status` stores resolution;
`orders.revision` is the optimistic concurrency token. `receipts` retains its
immutable request/result/facts JSON, case/action foreign keys, unique idempotency
key and one-closure-per-case index; resolution uses kind `closure`. The retailer
`messages` table now accepts only internal `note`/`escalation` kinds, with nullable
`recipient` retained and constrained to NULL. Customer turns live only in the
existing `conversation_turns` journal table. The journal Decision request format,
atomic attempt/outbox settlement and command-recovery records are unchanged.

The native catalog removes `send_customer_reply` and adds `resolve_case`. Do not
relabel the former as Read, delete historical tool records or adopt its pending
approvals. Review new classification, selectors, policy and cases normally. A
machine key does not grant policy-administration authority.

## Verification and rollout

Use deterministic native models and real installed SDK execution with HTTP
control-service doubles for offline checks. Verify conversation persistence with
no Decide, explicit closure allow/deny/pending, approved restart with the same
request/key, incomplete/foreign/stale resolution rejection, child settlement,
atomic receipts and exact replay. Exercise final ordinary responses after handoff
and resolution. Check old-state refusal without rewriting files and installed
wheel/sdist contents. Maintainer review covers scope, authorization and recovery
under the [repository review policy](../../../../MAINTAINERS.md#solo-maintainer-review).

Real-service and provider acceptance require a fresh Agent configured for the
current schema and a separate state directory. Verify the deployed schema and
policy before running acceptance cases. Preserve pending work under its original
runtime and identity; rollback must not reopen newer state with incompatible code.

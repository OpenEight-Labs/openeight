# Direct request preparation and recovery

This is an advanced reference for applications using the direct client. Start with
[the direct-client walkthrough](https://openeight.ai/docs/direct-sdk?language=python&integration=direct&execution=sync):
create a client, register tools, check readiness, request a Decision, and execute
only when authorized. The fragment below assumes that setup is complete; it is
not a standalone application. Managed LangChain integrations handle these steps
for enrolled tool calls.

Prepare identity before sending when an acknowledgement might be lost. Use the
acknowledged `tool`, `tool_version`, exact-version `readiness` and policy-selected
`selected_values` from your registration/readiness flow:

```python
from openeight import prepare_decision, prepare_run_identity, RunCheckpoint

identity = prepare_run_identity(attributes={"job": "example"})
run = client.governed_run(identity=identity)
prepared = prepare_decision(
    run_id=run.run_id,
    tool_id=tool.id,
    tool_version_id=tool_version.id,
    input={},
    policy_contract_set_digest=readiness.policy_contract_set_digest,
    selected_values=selected_values,
)
result = client.decide_prepared(prepared)
# The application checks authorization before performing an external action.
checkpoint = run.suspend()
saved = checkpoint.to_dict()
run = client.resume_governed_run(RunCheckpoint.from_dict(saved))
run.close()
```

`PreparedDecision` snapshots selected values and allocates identity/time once.
Retries use the same value and authenticated machine actor, including the same API
key identity. Changing credentials can cause a server replay conflict even for the
same Agent name. Neither the checkpoint nor the prepared request contains credentials.

Initial `allow` authorizes execution. After `approval_required`, observe `approved`
for that exact Decision, then replay the unchanged prepared request to revalidate
current authorization. Require matching identities, `replayed=True` and verdict
`approval_required` or `allow`. Approval does not change the stored verdict. Other
states/verdicts and replay errors refuse execution; the direct client never polls or executes
an external action automatically.

`close()` seals Event admission. A failed Event flush or uncertain completion can
be retried on the same Run before closing its client. The completion timestamp and
request remain unchanged. Acknowledged completion is never resent after cleanup
failure; terminal cleanup errors may persist. Client shutdown remains terminal.

Checkpoints contain suspension facts, not queued Events, storage or distributed
fencing. Resume under exclusive application ownership and the original endpoint,
Agent and Workspace authority. Locally owned or completion-attempted Runs cannot
resume. Repeated suspend/resume cycles are supported.

New checkpoints retain `attributes_json` verbatim so numeric spelling and key order
survive Python/TypeScript transfer. The `attributes` property returns a copy.
Legacy version-1 checkpoints without encoded attributes remain accepted and export
without that field, but exact cross-language replay cannot be guaranteed. Server
identity conflicts remain refusals.

Optional `workflow_id`/`workflow_version` are paired UUIDv5/v7 and SemVer references
(up to 128 UTF-8 bytes), carried unchanged through Run and Decision writes. They
do not provision a workflow or grant authority. Timestamps normalize to UTC
microseconds; Decision source times exclude the zero instant at year 1.


Completion captures the later of startup and the local clock once, avoiding a
negative duration after clock skew. Retries keep that timestamp unchanged.

See also [raw telemetry](raw-telemetry.md) and the [SDK overview](../README.md).

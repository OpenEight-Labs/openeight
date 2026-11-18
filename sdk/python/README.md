# OpenEight Python SDK

OpenEight adds observation, policy enforcement and human approval to AI agents.
Build new agents or connect existing ones using LangChain, Deep Agents, or the
direct Python client.

[Documentation](https://openeight.ai/docs) · [Get started](https://openeight.ai/docs/connect-agent?language=python&integration=langchain&execution=sync) · [Compatibility](https://openeight.ai/docs/compatibility?language=python&integration=langchain&execution=sync)

## Installation

The initial public release is being prepared. Publication and registry acceptance
are pending; the commands below apply once the release is available on PyPI.

```bash
pip install openeight
```

Or, in a uv project:

```bash
uv add openeight
```

Managed LangChain and Deep Agents integrations support standard CPython
3.11–3.14. LangChain dependencies are included; see
[Compatibility](https://openeight.ai/docs/compatibility?language=python&integration=langchain&execution=sync)
for optional integrations and supported versions.

## Choose your integration

- **[LangChain](https://openeight.ai/docs/langchain?language=python&integration=langchain&execution=sync)** — build tool-calling agents with OpenEight-managed governance and observation.
- **[Deep Agents](https://openeight.ai/docs/deep-agents?language=python&integration=deepagents&execution=sync)** — govern supported Deep Agents workflows, including declarative child agents.
- **[Direct Python SDK](https://openeight.ai/docs/direct-sdk?language=python&integration=direct&execution=sync)** — authorize tool attempts and report execution from your own application logic.

## Quick start

Define your model and tools, or reuse them from an existing application.
Follow [Get started](https://openeight.ai/docs/connect-agent?language=python&integration=langchain&execution=sync)
to create an Agent API key and set `OPENEIGHT_API_KEY`.

```python
import os

from openeight import OpenEightConfig
from openeight.langchain import create_agent

# Supply your configured model and tool functions.
agent = create_agent(
    model=model,
    tools=tools,
    openeight=OpenEightConfig(
        endpoint="https://app.openeight.ai",
        agent_name="payments-worker",
        credential=os.environ["OPENEIGHT_API_KEY"],
    ),
)

result = agent.invoke({"messages": [("user", "Hello")]})
```

Configure tool permissions and activate policies in the console before governed
tools execute. Remote OpenEight endpoints require HTTPS.

For a complete runnable application, see the
[refund-agent example](https://github.com/OpenEight-Labs/openeight/tree/main/cookbook/patterns/governed-refund).

## Documentation

- **Guides:** [Sessions and invocation](https://openeight.ai/docs/guides/sessions?language=python&integration=langchain&execution=sync), [streaming and cleanup](https://openeight.ai/docs/guides/streaming?language=python&integration=langchain&execution=sync), and [threads and checkpoints](https://openeight.ai/docs/guides/checkpoints?language=python&integration=langchain&execution=sync).
- **SDK reference:** [Constructors and execution](https://openeight.ai/docs/reference/constructors?language=python&integration=langchain&execution=sync), and [configuration, approval waiting and content capture](https://openeight.ai/docs/reference/configuration?language=python&integration=langchain&execution=sync).

Completion captures the later of startup and the local clock once, avoiding a
negative duration after clock skew. Retries keep that timestamp unchanged.

## Direct request preparation and recovery

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
states/verdicts and replay errors refuse execution; the SDK never polls or executes
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

### Raw telemetry acceptance

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
Framework compatibility declarations and managed integrations are unchanged.

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

## Type checking

The distribution includes `py.typed`. Direct-client methods, managed lifecycle
methods and catalog results expose inline type annotations. Native agent state,
model output and application context remain framework-defined values (`Any`);
validate those with your application's state or response schema.

From `sdk/python/`, run `python -m mypy typing_tests` after installing the `dev`
extra. These consumer checks verify both result types and rejected invalid calls.
CI checks them against the built SDK wheel, including ordinary and durable APIs.

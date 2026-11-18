# Migrate an existing agent to OpenEight

For a complete runnable before/after example, start with
[Run native, then add governance](build-or-integrate.md#run-native-then-add-governance).
It uses the same refund tools and synthetic data on both sides of the migration.

Use this guide to add OpenEight observation and governance to an existing Python
LangChain or Deep Agents application. Keep your business tools and orchestration;
change how the agent is constructed, configure its published tools, and verify
that blocked actions never execute.

The managed SDK constructs the native agent. It does not attach to an already
compiled graph. If your application exposes only a compiled agent, find its
original construction code before proceeding.

## 1. Check your starting point

| Existing application | Migration path |
| --- | --- |
| LangChain `create_agent` | Use the LangChain constructor replacement below. |
| Deep Agents with dictionary-based subagents | Use the Deep Agents constructor replacement below. |
| Compiled LangGraph graph or Deep Agents `CompiledSubAgent` | No managed attachment path currently exists. Reconstruct through supported declarations where possible; otherwise integrate the low-level SDK at your application's effect boundaries. |
| Legacy LangChain `AgentExecutor`, chains or `create_react_agent` | Migrate to the modern `create_agent` API first, or use explicit low-level SDK integration. |

Upstream Deep Agents supports compiled subagents, but OpenEight's current managed
constructor does not. A tool that calls another agent also does not automatically
expose that child's internal tools to governance. Each effect boundary needs its
own supported integration. See the public
[Deep Agents subagent documentation](https://docs.langchain.com/oss/python/deepagents/subagents)
and [LangChain supervisor pattern](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents).

For older applications, follow the
[LangChain v1 migration guide](https://docs.langchain.com/oss/python/migrate/langchain-v1)
for changes to prompts, hooks, state and runtime context.

## 2. Install and configure

In your application's existing virtual environment:

```bash
pip install 'openeight==0.0.1'
# Or: uv add 'openeight==0.0.1'

# For Deep Agents:
pip install 'openeight[deepagents]==0.0.1'
# Or: uv add 'openeight[deepagents]==0.0.1'
```

This guide targets the locally qualified SDK 0.0.1 combination: LangChain 1.4.3,
langchain-core 1.6.7, LangGraph 1.2.14 and Deep Agents 0.7.23, on conventional
CPython 3.11–3.14. Resolve dependency conflicts in your application's lockfile;
do not assume that every version described in the latest upstream docs is
supported. Keep your existing model-provider adapter and provider credentials.

Create an Agent in OpenEight with the matching integration, then supply its
endpoint, immutable name and bound API key through your application's secret
configuration:

```text
OPENEIGHT_ENDPOINT=https://<your-openeight-api-origin>
OPENEIGHT_AGENT_NAME=<your-agent-name>
OPENEIGHT_API_KEY=<that-agents-machine-key>
```

Use those values in both constructor examples below:

```python
import os
from openeight import OpenEightConfig

config = OpenEightConfig(
    endpoint=os.environ["OPENEIGHT_ENDPOINT"],
    agent_name=os.environ["OPENEIGHT_AGENT_NAME"],
    credential=os.environ["OPENEIGHT_API_KEY"],
    on_block="raise",
)
```

`on_block="raise"` makes authorization blocks explicit to your application. Model
credentials remain separate from the OpenEight API key. Agent construction does
not send network requests.

## 3. Replace the constructor

### LangChain

Before:

```python
from langchain.agents import create_agent

agent = create_agent(
    model=model,
    tools=tools,
    system_prompt=system_prompt,
    middleware=middleware,
)
```

After, using the same application objects:

```python
from openeight.langchain import create_agent

agent = create_agent(
    model=model,
    tools=tools,
    system_prompt=system_prompt,
    middleware=middleware,
    openeight=config,
)
```

Retain supported `state_schema`, `context_schema`, `response_format`, saver and
store arguments where used. Verify their exact implementations before relying on
checkpoint continuation. Middleware may still implement your application logic,
but arbitrary model replacement needs migration to explicitly enrolled runtime
model choices. Do not install another governance middleware alongside this factory.
LangChain documents its native extension points in
[custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom).

### Deep Agents

Before:

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
    tools=coordinator_tools,
    system_prompt=system_prompt,
    subagents=subagents,
    backend=backend,
)
```

After, retaining your supported declarative children and backend:

```python
from openeight.deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
    tools=coordinator_tools,
    system_prompt=system_prompt,
    subagents=subagents,
    backend=backend,
    openeight=config,
)
```

Children remain dictionaries declaring their name, description, prompt, tools and
optional model. Give each child only its intended tools. OpenEight discovers their
actual catalog bindings, including generated native tools. An authorized
delegation does not authorize a child's write.

Review the effective general-purpose child and filesystem tools rather than
assuming your business tools are the entire catalog. The SDK offers
`include_general_purpose=False` when your application intentionally excludes that
child. It also reserves core harness middleware, so upstream middleware overrides
are not universally interchangeable. These are compatibility checks on your
existing topology, not a reason to redesign it.

## 4. Configure the tools

`invoke()` and `ainvoke()` automatically publish tool schemas before model
execution. You do not need to call `publish()` before every invocation.

For initial setup, you can optionally publish without invoking the model so that
you can review the catalog and activate policies before the first agent run:

```python
agent.publish()
print(agent.delivery_status().catalog_delivery_state)
```

Wait for catalog acknowledgment, then review the actual tool bindings in the
OpenEight console. Classify reads and writes, select the required policy inputs,
and test and activate the intended policies. For Deep Agents, inspect every child
binding and generated native tool.

Publication acknowledgment is not policy readiness or write authorization.
Invocation checks still apply. Keep destination authentication, application scope
validation and any customer consent in your own tools.

## 5. Keep invocation, handle blocked execution

Ordinary `invoke` and `ainvoke` retain the native request shape and settle their
owned work automatically. A typical async application boundary becomes:

```python
from openeight import ApprovalTimeout, BlockedError

async def handle(request):
    try:
        return await agent.ainvoke(request)
    except (BlockedError, ApprovalTimeout) as error:
        return {"status": "blocked", "reason": error.state}
```

Adapt this response to your application's existing API. SDK/transport failures
must also follow your error path; never catch a governance failure and execute
the underlying tool directly. A blocked later action does not undo earlier
completed actions. Read business receipts before describing the overall outcome.

The default inline approval wait is one hour. Rejection, expiry and unsuccessful
replay do not execute the blocked tool. Configure an explicit timeout if your
request cannot wait that long. Inline approval waiting does not supply restart
recovery.

For streaming or a long-running service, keep one explicit owner context around
the operations:

```python
async def stream_response(request):
    async with agent:
        async for chunk in agent.astream(request):
            yield chunk
```

In a server, open that context at application startup and close it at shutdown.
Construct the owner after process fork and use it on its owning event loop. Close
abandoned streams before closing the owner. Keep tools and provider dependencies
alive until execution and SDK settlement finish.

## 6. Verify against your existing behavior

Use the same synthetic inputs and deterministic model responses for the original
and migrated agent. Verify that:

- The allowed path preserves tool arguments, business receipts and final output.
- Denied, unavailable and pending authorization leave the blocked tool's effects empty.
- For Deep Agents, an allowed delegation with a denied child write produces no child effect.
- Approved execution uses the exact authorized arguments; changed arguments need authorization.
- Streaming, cancellation and any existing checkpoint continuation preserve your application's guarantees.

Keep the current business idempotency and timeout-reconciliation behavior. An
OpenEight Decision is authorization, not proof that a payment, ticket or deployment
completed. Confirm completion using the destination's receipt.

Checkpoint adoption, durable approval recovery and runtime model selection have
additional contracts. Migrate them explicitly rather than assuming that replacing
the constructor preserves arbitrary persisted graphs. The
[checkpoint pattern](../patterns/checkpoint-recovery/) and the
[Customer Support operations guide](../agents/customer-support/docs/operations.md)
show application-owned recovery boundaries.

The cookbook's [Customer Support](../agents/customer-support/) and
[Incident Response](../agents/incident-response/) provide complete LangChain and
Deep Agents integrations. They are references; your migration can retain your
own use case, tools and data.

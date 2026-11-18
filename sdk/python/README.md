# OpenEight Python SDK

OpenEight checks policy before an agent calls a tool, pauses actions for human
approval when required, and records execution telemetry. For example, a support
agent can look up an order while a refund requires approval before it executes.

Use the managed LangChain or Deep Agents integrations, or add decision checks to
your own application with the direct Python client. Governance applies to calls
routed through OpenEight; it does not govern tools your application invokes outside
that integration.

[Website](https://openeight.ai) · [Documentation](https://openeight.ai/docs) · [Runnable examples](https://github.com/OpenEight-Labs/openeight/tree/main/cookbook) · [Report an issue](https://github.com/OpenEight-Labs/openeight/issues)

## Requirements

For the managed examples, use standard CPython 3.11–3.14 and an isolated virtual
environment. You also need:

- Access to an OpenEight deployment. The hosted service is configured through the
  [setup guide](https://openeight.ai/docs/connect-agent?language=python&integration=langchain&execution=sync);
  the server is not included in this package.
- A registered Agent and its bound OpenEight API key.
- Active policies for the tools you intend to run. The example below registers its
  tool before pausing so you can configure its policy in the Console.
- A model that supports tool calling, its provider integration package, and its
  provider credentials. These are separate from your OpenEight API key.

## Installation

```sh
python -m pip install openeight
```

Or, in a uv project:

```sh
uv add openeight
```

LangChain is included in the default installation. For Deep Agents, use
`python -m pip install 'openeight[deepagents]'` or `uv add 'openeight[deepagents]'`.
Model-provider packages are installed separately.

The SDK pins a qualified set of LangChain dependencies. Check
[compatibility](https://openeight.ai/docs/compatibility?language=python&integration=langchain&execution=sync)
before adding it to an existing application; independently upgrading those
framework dependencies can produce an unsupported combination. See
[optional integrations](https://github.com/OpenEight-Labs/openeight/blob/main/sdk/python/pyproject.toml)
for SQLite durability, PostgreSQL checkpoints and MCP extras.

## Quick start: govern an order lookup

This example uses OpenAI through `langchain-openai`, with an explicitly selected
model. The tool reads synthetic data; it does not contact an order service or
modify an order. The model and OpenEight calls are real network requests.

Install the provider integration used by the cookbook:

```sh
python -m pip install 'langchain-openai==1.6.7'
```

Set these variables in your local environment, replacing the placeholders:

```sh
export OPENEIGHT_ENDPOINT="https://app.openeight.ai"
export OPENEIGHT_AGENT_NAME="your-registered-agent-name"
export OPENEIGHT_API_KEY="your-openeight-agent-api-key"
export OPENAI_API_KEY="your-openai-api-key"
export OPENAI_MODEL="your-tool-capable-openai-model"
```

Save the following as `quickstart.py`:

```python
import os

from langchain.tools import tool
from langchain_openai import ChatOpenAI

from openeight import OpenEightConfig
from openeight.langchain import ApprovalConfig, create_agent


@tool
def lookup_order(order_id: str) -> str:
    """Look up the delivery status of a synthetic example order."""
    status = "delivered" if order_id == "ORD-1001" else "not found"
    print(f"Tool executed: {order_id} — {status}")
    return status


model = ChatOpenAI(
    model=os.environ["OPENAI_MODEL"],
    api_key=os.environ["OPENAI_API_KEY"],
)

with create_agent(
    model=model,
    tools=[lookup_order],
    system_prompt="Use lookup_order for order-status questions; do not guess.",
    openeight=OpenEightConfig(
        endpoint=os.environ["OPENEIGHT_ENDPOINT"],
        agent_name=os.environ["OPENEIGHT_AGENT_NAME"],
        credential=os.environ["OPENEIGHT_API_KEY"],
        on_block="raise",
        approval=ApprovalConfig(timeout=120),
    ),
) as agent:
    input("Activate a policy for lookup_order in the Console, then press Enter: ")
    status = agent.readiness()
    if status.unpublished or not status.readiness.fully_policy_ready:
        raise RuntimeError("Tool policies are not ready; check the Console.")
    result = agent.invoke(
        {"messages": [("user", "Look up the status of order ORD-1001.")]}
    )
    print(result["messages"][-1].content)
```

Run `python quickstart.py`. When it pauses, find `lookup_order` under your Agent
in the Console and activate a policy allowing this example lookup. Return to the
terminal and press Enter. If readiness is incomplete, finish configuring the
Agent and its tool policy before rerunning.

When the model calls the allowed tool, you will see
`Tool executed: ORD-1001 — delivered`, followed by the model's answer. The answer
wording can vary. Check the Agent's run and Decision in the Console as well:
a natural-language response alone does not prove a tool executed. The `with`
block owns the session and closes it on exit.

### Denial and human approval

Change the tool policy in the Console and rerun to exercise other outcomes:

| Outcome | Behavior in this example |
| --- | --- |
| Allow | Executes the lookup and returns its result to the model. |
| Deny | Does not execute the tool; raises `BlockedError` because `on_block="raise"`. |
| Approval required | Waits for human review, then revalidates authorization before execution. Rejection or expiration blocks the tool. |
| Approval wait times out | Raises `ApprovalTimeout` without executing the pending tool. |

The two-minute approval timeout keeps this terminal demo bounded. Approval must
come from an authorized reviewer in the Console; the agent cannot approve its own
request. For longer pauses or process restarts, use the
[checkpoint guide](https://openeight.ai/docs/guides/checkpoints?language=python&integration=langchain&execution=sync).

## Choose your integration

| Integration | Start here |
| --- | --- |
| Existing or new LangChain agent | [Managed LangChain guide](https://openeight.ai/docs/langchain?language=python&integration=langchain&execution=sync) |
| Deep Agents | [Deep Agents guide](https://openeight.ai/docs/deep-agents?language=python&integration=deepagents&execution=sync) |
| Your own execution loop | [Direct-client walkthrough](https://openeight.ai/docs/direct-sdk?language=python&integration=direct&execution=sync): register tools, check readiness, request decisions, and execute only when authorized. |
| A complete application | [Customer Support](https://github.com/OpenEight-Labs/openeight/tree/main/cookbook/agents/customer-support) or [governed refund](https://github.com/OpenEight-Labs/openeight/tree/main/cookbook/patterns/governed-refund) |

## Troubleshooting and compatibility

- **Authentication fails:** check the endpoint, registered Agent name and bound
  OpenEight key. A model-provider key cannot authenticate to OpenEight. Remote
  OpenEight endpoints require HTTPS.
- **Tools are not ready:** check tool registration and active policies in the
  Console. Registration alone does not establish policy readiness.
- **The tool does not execute:** inspect the Decision and approval state. Do not
  bypass a denial or pending approval by calling the tool directly.
- **Unsupported framework configuration:** compare installed versions and the
  integration you selected with the compatibility guide before upgrading packages.

Initial `0.0.x` releases are experimental and may introduce breaking changes.
Pin a version you have qualified and read the
[versioning policy](https://github.com/OpenEight-Labs/openeight/blob/main/docs/versioning.md)
before upgrading. Core package metadata accepts Python 3.11–3.15; managed
LangChain and Deep Agents qualification currently covers Python 3.11–3.14.
The package includes `py.typed`; model outputs and native application state still
need your application's own schema validation.

## Documentation and support

- [Sessions](https://openeight.ai/docs/guides/sessions?language=python&integration=langchain&execution=sync)
  and [streaming and cleanup](https://openeight.ai/docs/guides/streaming?language=python&integration=langchain&execution=sync).
- [Configuration, approval waiting and opt-in content capture](https://openeight.ai/docs/reference/configuration?language=python&integration=langchain&execution=sync).
- [Direct request preparation and recovery](https://github.com/OpenEight-Labs/openeight/blob/main/sdk/python/docs/direct-recovery.md)
  and [raw telemetry reference](https://github.com/OpenEight-Labs/openeight/blob/main/sdk/python/docs/raw-telemetry.md).
- [Support](https://github.com/OpenEight-Labs/openeight/blob/main/SUPPORT.md),
  [contributing](https://github.com/OpenEight-Labs/openeight/blob/main/CONTRIBUTING.md)
  and [private vulnerability reporting](https://github.com/OpenEight-Labs/openeight/blob/main/SECURITY.md).

## License

Copyright © 2026 OpenEight Systems Private Limited. Licensed under the
[Apache License, Version 2.0](https://github.com/OpenEight-Labs/openeight/blob/main/LICENSE).
Retain the included LICENSE and NOTICE files when redistributing. The SDK license
does not grant access to hosted services, which remain subject to separate terms.

# Build a new agent or govern an existing one

Both journeys use one small customer-support task: a customer received a damaged
order and requests a 10.50 USD refund from a 25 USD order. One LangChain agent has
two tools, `lookup_order` and `issue_refund`. A receipt leaves 14.50 USD refundable.
All orders and receipts are synthetic; no payment service is contacted.

| Your starting point | Start here |
| --- | --- |
| Building a new agent with OpenEight | [Build with OpenEight](#build-with-openeight) |
| You already have a LangChain agent | [Run native, then add governance](#run-native-then-add-governance) |

The [refund pattern](../patterns/governed-refund/) owns the code for both paths.
They share the business operations, tool schemas, prompt and model configuration.
The full [Customer Support application](../agents/customer-support/) adds multiple
remedies, a conversation UI and durable recovery; none is required here.

## Build with OpenEight

Use CPython 3.11–3.14. From the cookbook root:

```bash
python cookbook.py setup governed-refund
cp patterns/governed-refund/.env.example patterns/governed-refund/.env
```

Fill in the OpenEight endpoint, Agent name and that Agent's bound key in `.env`.
Set an explicit `AGENT_MODEL=provider:model` and `AGENT_MODEL_API_KEY` as well.
The requirements include the OpenAI provider adapter; install your provider's
adapter if you select another provider. OpenEight credentials and provider
credentials are separate.

Read [langchain_agent.py](../patterns/governed-refund/langchain_agent.py). Its
`build` function creates the agent through `openeight.langchain.create_agent`,
passing the shared tools, prompt, model and an `OpenEightConfig`.

For initial policy setup, optionally publish the schemas without invoking a model:

```bash
python cookbook.py run governed-refund -- publish
```

In OpenEight, review the published bindings, classify `lookup_order` as Read and
`issue_refund` as Write, select `amount` and `currency` as policy inputs, and test
and activate the intended policy. See the pattern's
[policy instructions](../patterns/governed-refund/docs/operations.md#configure-new-tools-once).
Publication alone is not policy readiness or authorization. Explicit publication
is optional because `invoke()` also publishes automatically before model execution.

Run the request:

```bash
python cookbook.py run governed-refund -- run ORD-1041 --amount 10.50
```

With an allowing policy and successful tool execution, the output has
`state: receipt_recorded` and one simulated 10.50 USD receipt. A denial leaves no
refund receipt. An approval-required policy pauses execution for review; pending,
rejected or expired approval does not execute the refund. Inspect the returned
state instead of treating every invocation as successful.

## Run native, then add governance

This is the customer's starting point: a native LangChain agent with working
business tools and no OpenEight SDK. From the cookbook root, create an independent
environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r patterns/governed-refund/requirements-native.txt
export AGENT_MODEL='provider:model'
export AGENT_MODEL_API_KEY='<your-model-provider-api-key>'
.venv/bin/python patterns/governed-refund/native_agent.py ORD-1041 --amount 10.50
```

This root `.venv` is separate from the governed pattern's environment created by
`cookbook.py setup`. Replace the model/key placeholders; these are not provider defaults. This path
uses only model credentials and does not read OpenEight settings. It runs a real
native LangChain graph. Output includes `governance: not_enabled` and reports a
receipt only when the tool actually produced one. It does not enforce OpenEight
policies or create OpenEight evidence.

The native constructor is in
[native_agent.py](../patterns/governed-refund/native_agent.py). Compare its `build`
function with [langchain_agent.py](../patterns/governed-refund/langchain_agent.py):

```diff
-from langchain.agents import create_agent
+from openeight.langchain import create_agent

 agent = create_agent(
     model=model,
     tools=build_tools(request),
     system_prompt=SYSTEM_PROMPT,
+    openeight=config,
 )
```

Configure `config` using your OpenEight Agent credentials. The governed runner
already implements this construction and blocked/approval handling, so follow
[Build with OpenEight](#build-with-openeight) to install the SDK in its separate
environment and run the same order and amount. Keep the existing tools, business
validation and prompt; do not copy or replace the business application.

In your own application, preserve its request/response handling and add explicit
handling for blocked execution and approval timeouts. The
[existing-agent migration guide](migrate-existing-agents.md) covers that code,
Deep Agents and compatibility limits. Constructor migration is not attachment to
an arbitrary already-compiled graph.

## Verify the migration

Native tests execute LangChain with deterministic model responses and no OpenEight
configuration. Paired tests check that native and governed agents produce the
same amount, currency and remaining balance when allowed, and that the governed
version produces no refund on denial. Existing tests cover approval outcomes.
CI also runs the native tests in an environment with no `openeight` package.

```bash
python cookbook.py test governed-refund -- -q
```

Each command starts with fresh in-memory data. Receipts do not survive process
exit; rerunning the demo is not recovery or payment idempotency. Real integration
must retain customer scope, exact request validation, destination idempotency and
stored receipts. Model prose and an authorization Decision are not proof of a
completed payment. The local tests do not certify live providers or services.
